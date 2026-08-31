"""
DOMAutopsy Web Server - FastAPI
================================
Lance qa_explorer en sous-process et streame en temps reel :
  - logs Python (stdout/stderr) via WebSocket /ws/logs/{run_id}
  - frames Chromium via CDP screencast vers WS /ws/screen/{run_id}
  - rapport HTML final accessible sur /report/{run_id}

Architecture pensee pour V2 (multi-run parallele) :
  - Chaque run a son port CDP unique (9222 + offset)
  - Chaque run a son run_id (uuid) qui scope les WS
  - RUNS dict global, thread-safe via asyncio (lock pas necessaire en single-thread asyncio)

Lancement :
  uvicorn server:app --reload --port 8000
"""

import asyncio
import sys
import logging
import subprocess

# Note Windows : on n'utilise PAS asyncio.create_subprocess_exec (qui necessite
# ProactorEventLoop, indisponible avec uvicorn --reload qui fork un worker en
# SelectorEventLoop). On utilise subprocess.Popen standard + lecture stdout via
# loop.run_in_executor. Compatible avec n'importe quel event loop, y compris uvloop.


# Filtrer les endpoints de polling silencieux des access logs uvicorn
class _QuietAccessFilter(logging.Filter):
    QUIET_PATHS = ("/api/runs", "/api/formats", "/api/providers", "/favicon.ico")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self.QUIET_PATHS)

logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Depends, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from typing import Optional
import json
import os
import socket
import aiohttp
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"

# Plage de ports CDP : 9222 pour le 1er run, 9223 pour le 2eme, etc.
CDP_PORT_BASE = 9222
CDP_PORT_RANGE = 50  # supporte jusqu'a 50 runs en parallele en theorie

# Etat global des runs
RUNS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan: startup (rien) -> serve -> shutdown (kill tous les subprocess)"""
    # --- Startup ---
    yield
    # --- Shutdown : kill tous les subprocess en cours ---
    active = sum(1 for r in RUNS.values() if r["proc"].poll() is None)
    print(f"[server] Shutdown : kill {active} runs actifs...")
    for rid, r in RUNS.items():
        proc = r["proc"]
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
    await asyncio.sleep(2)
    for rid, r in RUNS.items():
        proc = r["proc"]
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


app = FastAPI(title="DOMAutopsy Web", version="0.1.0", lifespan=lifespan)

# --- Auth Bearer token : 1 master + N tokens fils generes a la demande ---
# MASTER_TOKEN (env DOMAUTOPSY_API_TOKEN) : token immuable, donne tous les droits,
#   sert a generer des tokens fils via POST /api/auth/token. Si non set : no-auth.
# Tokens fils : generes a la volee, stockes en memoire avec label + expires_at,
#   peuvent etre revoques sans redemarrer. Accepteres comme le master.
# Persistance : in-memory uniquement. Au restart serveur les tokens fils sont
#   perdus (le master continue de marcher). Pour persistance permanente,
#   stocker en SQLite/Redis (V4).
import secrets
import time

MASTER_TOKEN = os.getenv("DOMAUTOPSY_API_TOKEN", "").strip() or None

# {token_value: {"label": str, "created_at": float, "expires_at": float | None, "scope": str}}
DYNAMIC_TOKENS: dict[str, dict] = {}


def _valid_token(token: str) -> Optional[dict]:
    """Verifie si un token est valide (master ou fils non expire). Retourne meta ou None."""
    if MASTER_TOKEN and token == MASTER_TOKEN:
        return {"label": "master", "scope": "admin", "expires_at": None}
    t = DYNAMIC_TOKENS.get(token)
    if not t:
        return None
    if t.get("expires_at") and time.time() > t["expires_at"]:
        # Expire, on le retire
        DYNAMIC_TOKENS.pop(token, None)
        return None
    return t


async def require_token(authorization: Optional[str] = Header(None)):
    """Dependency : valide Bearer (master OU token fils non expire)."""
    if not MASTER_TOKEN:
        return  # No-auth mode (pas de master = pas de auth)
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token in Authorization header")
    token = authorization.split(None, 1)[1].strip()
    if not _valid_token(token):
        raise HTTPException(403, "Invalid or expired Bearer token")


async def require_master(authorization: Optional[str] = Header(None)):
    """Dependency : exige le master token (operations d'admin sur les tokens)."""
    if not MASTER_TOKEN:
        raise HTTPException(503, "Master token not configured server-side (set DOMAUTOPSY_API_TOKEN env)")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token in Authorization header")
    token = authorization.split(None, 1)[1].strip()
    if token != MASTER_TOKEN:
        raise HTTPException(403, "Master token required for token management")


class TokenMintRequest(BaseModel):
    label: str
    ttl_seconds: Optional[int] = 3600   # 1h par defaut
    scope: Optional[str] = "user"       # user | readonly (cosmetique pour l'instant)


def find_free_cdp_port() -> int:
    """Trouve un port CDP libre dans la plage [9222, 9272]"""
    used_ports = {r["cdp_port"] for r in RUNS.values() if r.get("status") == "running"}
    for offset in range(CDP_PORT_RANGE):
        port = CDP_PORT_BASE + offset
        if port in used_ports:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Aucun port CDP libre dans la plage")


class RunRequest(BaseModel):
    url: str
    task: str
    output_format: str = "katalon"
    provider: str = "openai"
    model: Optional[str] = None
    min_wait: float = 2.0
    max_wait: float = 15.0
    network_idle: float = 3.0
    max_steps: int = 25
    headless: bool = True   # defaut headless pour multi-run sans chaos d'ecran


# Cache formats au boot (pas besoin de recharger a chaque requete)
_FORMATS_CACHE: Optional[dict] = None
def _get_formats_cached() -> dict:
    global _FORMATS_CACHE
    if _FORMATS_CACHE is None:
        from qa_explorer import OUTPUT_FORMATS
        _FORMATS_CACHE = {
            k: {"label": v["label"], "extension": v["extension"]}
            for k, v in OUTPUT_FORMATS.items()
        }
    return _FORMATS_CACHE


@app.get("/", response_class=HTMLResponse)
async def index():
    """Sert l'UI principale"""
    html_path = WEB_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(404, "index.html introuvable dans web/")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/auth/token", dependencies=[Depends(require_master)])
async def mint_token(req: TokenMintRequest):
    """Genere un token fils dynamique (Bearer Auth = master token requis).

    Body: {label, ttl_seconds, scope}
    Retourne: {token, label, expires_at, scope}

    Use case GitHub Action :
        # job de setup (1 fois en debut de pipeline) :
        TOKEN=$(curl -X POST https://dom.example.com/api/auth/token \\
          -H "Authorization: Bearer $MASTER_TOKEN" \\
          -d '{"label":"gh-action-run-${{ github.run_id }}","ttl_seconds":7200}' | jq -r .token)
        # jobs suivants utilisent $TOKEN, et il s'auto-detruit dans 2h.
        # Pas besoin de revoquer manuellement.
    """
    if not req.label.strip():
        raise HTTPException(400, "label vide")
    ttl = req.ttl_seconds if req.ttl_seconds and req.ttl_seconds > 0 else 3600
    token_value = secrets.token_urlsafe(32)  # 256 bits d'entropie
    expires_at = time.time() + ttl
    DYNAMIC_TOKENS[token_value] = {
        "label": req.label.strip()[:80],
        "created_at": time.time(),
        "expires_at": expires_at,
        "scope": req.scope or "user",
    }
    return {
        "token": token_value,
        "label": req.label.strip()[:80],
        "expires_at": expires_at,
        "expires_in_s": ttl,
        "scope": req.scope or "user",
    }


@app.get("/api/auth/tokens", dependencies=[Depends(require_master)])
async def list_tokens():
    """Liste les tokens fils actifs (sans exposer leur valeur). Master only."""
    now = time.time()
    # Nettoie les expires au passage
    expired = [t for t, m in DYNAMIC_TOKENS.items() if m.get("expires_at") and m["expires_at"] < now]
    for t in expired:
        DYNAMIC_TOKENS.pop(t, None)
    return [
        {
            "label": m["label"],
            "created_at": m["created_at"],
            "expires_at": m["expires_at"],
            "expires_in_s": int(m["expires_at"] - now) if m.get("expires_at") else None,
            "scope": m.get("scope", "user"),
            # On expose 8 derniers chars du token pour permettre de l'identifier sans le compromettre
            "token_suffix": t[-8:],
        }
        for t, m in DYNAMIC_TOKENS.items()
    ]


@app.delete("/api/auth/token/{token_suffix}", dependencies=[Depends(require_master)])
async def revoke_token(token_suffix: str):
    """Revoque un token fils par son suffix (les 8 derniers chars). Master only."""
    if len(token_suffix) < 4:
        raise HTTPException(400, "Suffix trop court (min 4 chars)")
    matched = [t for t in DYNAMIC_TOKENS.keys() if t.endswith(token_suffix)]
    if not matched:
        raise HTTPException(404, f"Aucun token actif avec suffix '{token_suffix}'")
    for t in matched:
        DYNAMIC_TOKENS.pop(t, None)
    return {"revoked": len(matched)}


@app.get("/api/auth/me")
async def whoami(authorization: Optional[str] = Header(None)):
    """Retourne les infos du token courant (utile pour le CLI / debug)."""
    if not MASTER_TOKEN:
        return {"auth_enabled": False, "label": None}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(None, 1)[1].strip()
    meta = _valid_token(token)
    if not meta:
        raise HTTPException(403, "Invalid or expired Bearer token")
    return {"auth_enabled": True, **meta}


@app.get("/health")
async def health():
    """Health check pour reverse proxy (Caddy) + sondes K8s liveness/readiness."""
    active = sum(1 for r in RUNS.values() if r.get("proc") and r["proc"].poll() is None)
    return {
        "status": "ok",
        "active_runs": active,
        "total_runs_inmem": len(RUNS),
        "screencast_hubs": len(SCREENCAST_HUBS),
        "auth_enabled": MASTER_TOKEN is not None,
        "dynamic_tokens": len(DYNAMIC_TOKENS),
    }


@app.get("/api/formats")
async def list_formats():
    """Retourne les formats de sortie supportes (cache en memoire)"""
    return _get_formats_cached()


@app.get("/api/providers")
async def list_providers():
    """Retourne les providers LLM supportes"""
    from qa_explorer import PROVIDERS
    # Ne renvoie pas les cles API, juste les metadonnees utiles au frontend
    return {
        k: {
            "default_model": v["default_model"],
            "env_var": v["env_var"],
            "key_present": bool(os.getenv(v["env_var"])),
        }
        for k, v in PROVIDERS.items()
    }


@app.get("/api/runs")
async def list_runs():
    """Retourne tous les runs (actifs + termines) - utile pour la sidebar / monitoring"""
    return [
        {
            "run_id": rid,
            "status": r["status"],
            "cdp_port": r["cdp_port"],
            "url": r.get("url"),
            "task": (r.get("task") or "")[:80],
            "output_format": r.get("output_format"),
            "report_path": r.get("report_path"),
        }
        for rid, r in RUNS.items()
    ]


def _kill_process_tree(pid: int, timeout: float = 3.0) -> dict:
    """R7 : tue le processus ET tous ses descendants (Chromium, Playwright,
    browser-use spawn plusieurs enfants qui survivent au kill du parent).

    Sequence : SIGTERM parent + enfants -> attend timeout -> SIGKILL survivants.
    Utilise psutil (deja installe via browser-use) : cross-OS Windows/Linux/Mac.

    Retourne {killed_pids, still_alive} pour audit.
    """
    try:
        import psutil
    except ImportError:
        # Fallback : plate-forme sans psutil, on kill juste le parent
        try:
            import os as _os, signal as _sig
            _os.kill(pid, _sig.SIGKILL if hasattr(_sig, "SIGKILL") else _sig.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        return {"killed_pids": [pid], "still_alive": [], "psutil": False}

    killed: list[int] = []
    still_alive: list[int] = []
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"killed_pids": [], "still_alive": [], "psutil": True}

    # 1. Collecte l'arbre AVANT terminate (enfants disparaissent au parent kill)
    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []

    all_procs = children + [parent]

    # 2. Terminate propre d'abord (arret gracieux)
    for p in all_procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 3. Attend jusqu'a timeout
    gone, alive = psutil.wait_procs(all_procs, timeout=timeout)
    killed.extend(p.pid for p in gone)

    # 4. Kill dur pour les survivants
    for p in alive:
        try:
            p.kill()
            killed.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            still_alive.append(p.pid)

    return {"killed_pids": killed, "still_alive": still_alive, "psutil": True}


def _mark_run_stopped_on_disk(run_dir_str: str | None) -> None:
    """Ecrit status="stopped" dans meta.json du run stoppe (fix R7).
    Sans ca, /api/history continuerait a afficher le run comme actif."""
    if not run_dir_str:
        return
    p = Path(run_dir_str) / "meta.json"
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            data = {}
        data["status"] = "stopped"
        data["stopped_at"] = __import__("datetime").datetime.now().isoformat()
        data.setdefault("pipeline_status", "interrupted")
        data.setdefault("agent_status", "interrupted")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[server] _mark_run_stopped_on_disk({run_dir_str}) : {e}")


@app.delete("/api/run/{run_id}", dependencies=[Depends(require_token)])
async def kill_run(run_id: str):
    """Tue COMPLETEMENT un run : subprocess Python + descendants Chromium/
    browser-use, WS+screencast, met a jour meta.json status=stopped.
    R7 fix : ancien code faisait juste proc.terminate() qui laissait les
    enfants Chromium vivants indefiniment sur Windows."""
    if run_id not in RUNS:
        raise HTTPException(404, f"run_id inconnu: {run_id}")
    r = RUNS[run_id]
    proc: subprocess.Popen = r["proc"]
    loop = asyncio.get_running_loop()
    tree_report = {"killed_pids": [], "still_alive": []}
    if proc.poll() is None:
        tree_report = await loop.run_in_executor(None, _kill_process_tree, proc.pid, 3.0)
        try:
            await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=2)
        except (asyncio.TimeoutError, Exception):
            pass

    # Ferme le hub screencast s'il tourne encore pour ce run
    hub = SCREENCAST_HUBS.pop(run_id, None)
    if hub is not None:
        hub.stopped = True
        if hub.task:
            hub.task.cancel()

    # meta.json status=stopped (R7)
    _mark_run_stopped_on_disk(r.get("run_dir"))

    r["status"] = "stopped"
    r["stop_report"] = tree_report
    return {
        "run_id": run_id,
        "status": "stopped",
        "killed_pids": tree_report.get("killed_pids", []),
        "still_alive": tree_report.get("still_alive", []),
    }


class PlaywrightRunRequest(BaseModel):
    project_dir: str         # chemin absolu vers le projet Playwright (contient playwright.config.*)
    target: Optional[str] = None    # fichier/dossier specifique (ex: tests/login.spec.ts) - optionnel
    args: Optional[str] = None      # args bruts (ex: '--workers 4 --grep login --headed')
    headless: bool = True           # par defaut headless (impose --reporter=line)


@app.post("/api/playwright/run", dependencies=[Depends(require_token)])
async def run_playwright(req: PlaywrightRunRequest):
    """Lance 'npx playwright test ...' dans le projet Playwright fourni.
    Stream stdout via WS /ws/logs/{run_id} comme tout autre run.
    Pas de screencast (pas de CDP unique : npx playwright peut lancer N workers).
    Le rapport HTML natif de Playwright (playwright-report/) reste dans le projet
    du user - on copie aussi un meta.json + un summary dans runs/<ts>_pwtest_<id>/.
    """
    project_dir = Path(req.project_dir).expanduser().resolve()
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(400, f"Repertoire de projet introuvable : {project_dir}")
    has_config = any((project_dir / f).exists() for f in [
        "playwright.config.ts", "playwright.config.js", "playwright.config.mjs", "playwright.config.cjs"
    ])
    has_pkg = (project_dir / "package.json").exists()
    if not has_config and not has_pkg:
        raise HTTPException(400, f"Pas de playwright.config.* ni package.json dans {project_dir}")

    run_id = uuid4().hex[:12]
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = RUNS_DIR / f"{ts}_pwtest_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build command : npx playwright test [target] [args]
    # On force --reporter=list pour avoir un output lisible en stream.
    # Si l'user a deja un reporter dans args, le sien est conserve (Playwright accepte plusieurs).
    cmd = ["npx", "playwright", "test"]
    if req.target:
        cmd.append(req.target)
    user_args = (req.args or "").strip().split() if req.args else []
    cmd.extend(user_args)
    # Headless est le default Playwright ; pour le forcer on n'ajoute pas --headed.
    # Si l'utilisateur veut headed il met --headed dans args.
    # On ajoute --reporter=list si aucun reporter explicite dans les args du user
    if not any(a.startswith("--reporter") for a in user_args):
        cmd.append("--reporter=list")

    # Windows : npx est un .cmd, il faut shell=True OU passer par cmd /c.
    # On utilise shell=True UNIQUEMENT sur Windows et avec une commande sans interpolation user shell.
    if sys.platform == "win32":
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        proc = subprocess.Popen(
            cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(project_dir), bufsize=1,
        )
    else:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(project_dir), bufsize=1,
        )

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    RUNS[run_id] = {
        "proc": proc,
        "cdp_port": None,
        "log_queue": log_queue,
        "status": "running",
        "report_path": None,
        "url": None,
        "task": f"npx playwright test {req.target or '(all tests)'}",
        "output_format": "playwright_native",
        "run_dir": str(out_dir),
        "timestamp": ts,
        "is_playwright_runner": True,
        "project_dir": str(project_dir),
        "cmd": " ".join(cmd) if isinstance(cmd, list) else cmd,
    }
    asyncio.create_task(_pump_stdout(run_id, proc, log_queue))
    # Ecrit immediatement un meta.json minimal pour l'historique
    try:
        (out_dir / "meta.json").write_text(json.dumps({
            "timestamp": ts,
            "started_at": __import__("datetime").datetime.now().isoformat(),
            "scenario_url": None,
            "scenario_name": f"Playwright suite: {req.target or 'all'}",
            "task": f"npx playwright test {req.target or ''} {req.args or ''}".strip(),
            "output_format": "playwright_native",
            "provider": "none",
            "model": "npx-playwright",
            "headless": req.headless,
            "is_playwright_runner": True,
            "project_dir": str(project_dir),
            "status": "running",
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[server] meta.json initial echec : {e}")
    return {"run_id": run_id, "cdp_port": None, "is_playwright_runner": True, "cmd": RUNS[run_id]["cmd"]}


@app.post("/api/replay/{run_id}", dependencies=[Depends(require_token)])
async def replay_run(run_id: str, headless: bool = True):
    """Rejoue un run historique. Deux moteurs :

    - PRIMAIRE : `npx playwright test <spec-relative> --workers=1
      --output=<replay_dir>` quand test_playwright.spec.ts est present dans
      le dossier source. Les 2 reporters (list stream + json fichier) sont
      declares dans playwright.config.ts, pas en CLI (sinon --reporter=list
      remplacerait tout et le JSON ne serait pas produit). Le fichier JSON
      atterrit dans <replay_dir>/replay_results.json via l'env var
      DOMAUTOPSY_REPLAY_JSON. C'est le format canonique produit
      systematiquement par qa_explorer (schema v2.0). Ce chemin est le
      moteur normal a partir du refactor Aout 2026.

    - LEGACY FALLBACK : qa_player.py (Playwright pur Python, click+input
      seulement) uniquement pour les anciens runs qui n'ont pas encore de
      TS genere. Signale explicitement `engine: qa_player_legacy` dans le
      meta.json et log un WARNING clair.

    Reponse au cahier : "Pour les anciens runs qui ne possedent pas de
    TypeScript genere, conserve provisoirement qa_player.py comme fallback
    legacy ou genere leur TypeScript a la volee. Le fallback doit etre
    explicitement indique dans les logs et dans meta.json."
    """
    # Trouver le dossier source
    source_dir = _find_run_dir(run_id)
    if source_dir is None or not source_dir.exists():
        raise HTTPException(404, f"Run source introuvable: {run_id}")
    clean_steps_file = source_dir / "clean_steps.json"
    if not clean_steps_file.exists():
        raise HTTPException(400, f"clean_steps.json absent dans {source_dir.name}, replay impossible")

    # Ne jamais lancer sciemment un TS partiel. Les steps de lecture/bruit
    # peuvent etre SKIPPED, mais une interaction exclue faute de preuve live
    # porte replay_blocking=true et invalide l'artefact canonique.
    try:
        clean_payload = json.loads(clean_steps_file.read_text(encoding="utf-8"))
        blocking_steps = [
            step for step in (clean_payload.get("steps") or [])
            if isinstance(step, dict) and step.get("replay_blocking") is True
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"clean_steps.json invalide: {exc}") from exc
    if blocking_steps:
        raise HTTPException(
            409,
            f"Replay refuse: {len(blocking_steps)} interaction(s) sans preuve deterministe; recapture requise",
        )

    try:
        source_meta = json.loads((source_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source_meta = {}
    unsupported_count = int(source_meta.get("playwright_unsupported_count") or 0)
    if unsupported_count:
        raise HTTPException(
            409,
            f"Replay refuse: {unsupported_count} step(s) Playwright non traduisible(s)",
        )

    replay_id = uuid4().hex[:12]
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    replay_dir = RUNS_DIR / f"{ts}_replay_{replay_id}"
    replay_dir.mkdir(parents=True, exist_ok=True)

    spec_file = source_dir / "test_playwright.spec.ts"
    use_playwright_ts = spec_file.exists()

    proc: subprocess.Popen
    engine: str
    cmd_repr: str
    fallback_reason: Optional[str] = None
    cdp_port_alloc: Optional[int] = None

    if use_playwright_ts:
        # --- MOTEUR PRIMAIRE : npx playwright test sur le TS canonique ---
        # Chemin RELATIF au repo (Playwright interprete un chemin absolu
        # Windows type "C:\..." comme une expression de filtrage a cause du
        # ":" et des antislashs).
        try:
            spec_rel = spec_file.relative_to(ROOT).as_posix()
        except ValueError:
            # source_dir est en dehors du repo (cas theorique, on tombe en
            # fallback plutot que passer un chemin absolu risque)
            spec_rel = None

        if spec_rel is None:
            use_playwright_ts = False
            fallback_reason = "spec en dehors du repo, impossible de passer un chemin relatif a `npx playwright test`"
        else:
            # Un seul worker pour un replay deterministe, reporter list pour
            # que le stdout ligne-a-ligne soit exploitable en streaming, et
            # --output isole les artefacts (traces, videos) dans le run dir.
            output_rel = replay_dir.relative_to(ROOT).as_posix()
            # Pas de --reporter en CLI : les deux reporters (list + json)
            # sont declares dans playwright.config.ts. On pilote juste le
            # chemin du reporter json via l'env var DOMAUTOPSY_REPLAY_JSON
            # pour que le fichier atterrisse dans le replay_dir et soit
            # exploitable par report_generator pour rapprocher chaque
            # test.step('[step-XXXX] ...') a son verdict Playwright.
            replay_json_path = replay_dir / "replay_results.json"
            env = dict(os.environ)
            env["DOMAUTOPSY_REPLAY_JSON"] = str(replay_json_path)

            # Runtime resolution : embarque (autonome a l'execution) vs dev (npx global du PATH)
            rt = _resolve_embedded_runtime()
            # D8 : headless=False -> --headed. Auparavant le flag arrivait dans
            # la request mais etait ignore par npx (default Playwright = headless).
            extra_flags = [] if headless else ["--headed"]
            if rt:
                # Mode embarque : node.exe local + cli.js Playwright + browsers/
                cmd = [
                    rt["node"], rt["cli"], "test", spec_rel,
                    "--workers=1",
                    f"--output={output_rel}",
                    *extra_flags,
                ]
                env["PLAYWRIGHT_BROWSERS_PATH"] = rt["browsers"]
                runtime_mode = "embedded"
                # Pas besoin de shell=True : node.exe est un vrai exe
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    cwd=str(ROOT), bufsize=1, env=env,
                )
            else:
                # Mode dev : fallback npx global + cache Playwright utilisateur
                cmd = [
                    "npx", "playwright", "test", spec_rel,
                    "--workers=1",
                    f"--output={output_rel}",
                    *extra_flags,
                ]
                runtime_mode = "system_npx"
                # Windows : npx est un .cmd, il faut passer par shell=True avec
                # une commande sans interpolation user. Meme pattern que
                # /api/playwright/run.
                if sys.platform == "win32":
                    cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
                    proc = subprocess.Popen(
                        cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        cwd=str(ROOT), bufsize=1, env=env,
                    )
                else:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        cwd=str(ROOT), bufsize=1, env=env,
                    )
            engine = "playwright_ts"
            cmd_repr = " ".join(str(c) for c in cmd)

    if not use_playwright_ts:
        # --- FALLBACK LEGACY : qa_player.py ---
        cdp_port_alloc = find_free_cdp_port()
        args = [
            sys.executable, "-u", str(ROOT / "qa_player.py"),
            "--run-dir", str(source_dir),
            "--output-dir", str(replay_dir),
            "--port", str(cdp_port_alloc),
        ]
        if headless:
            args.append("--headless")
        engine = "qa_player_legacy"
        fallback_reason = fallback_reason or (
            "test_playwright.spec.ts absent dans le run source "
            "(run pre-refactor Aout 2026)"
        )
        cmd_repr = " ".join(args)
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(ROOT), bufsize=1,
        )

    # meta.json initial (le detail final est ecrit par le subprocess)
    try:
        meta_engine_details = {
            "engine": engine,
            "legacy_fallback": engine == "qa_player_legacy",
            "legacy_fallback_reason": fallback_reason,
        }
        if engine == "playwright_ts":
            meta_engine_details["runtime_mode"] = runtime_mode  # "embedded" | "system_npx"
        (replay_dir / "meta.json").write_text(json.dumps({
            "timestamp": ts,
            "started_at": __import__("datetime").datetime.now().isoformat(),
            "scenario_url": None,
            "scenario_name": f"Replay of {run_id}",
            "task": f"Replay run {run_id} via {engine}",
            "output_format": "replay",
            "provider": "none",
            "model": ("npx-playwright" if engine == "playwright_ts" else "playwright-python-pure"),
            "headless": headless,
            "is_replay": True,
            "source_run_id": run_id,
            "cmd": cmd_repr,
            "status": "running",
            **meta_engine_details,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[server] meta.json initial replay echec : {e}")

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    RUNS[replay_id] = {
        "proc": proc,
        "cdp_port": cdp_port_alloc,   # None si engine=playwright_ts (pas de CDP unique)
        "log_queue": log_queue,
        "status": "running",
        "report_path": None,
        "url": None,
        "task": f"Replay of {run_id} via {engine}",
        "output_format": "replay",
        "run_dir": str(replay_dir),
        "timestamp": ts,
        "source_run_id": run_id,
        "is_replay": True,
        "engine": engine,
        "legacy_fallback": engine == "qa_player_legacy",
        "cmd": cmd_repr,
    }
    asyncio.create_task(_pump_stdout(replay_id, proc, log_queue))

    if fallback_reason:
        print(f"[server] /api/replay/{run_id} -> FALLBACK LEGACY qa_player.py : {fallback_reason}")

    return {
        "run_id": replay_id,
        "cdp_port": cdp_port_alloc,
        "source_run_id": run_id,
        "replay_dir": replay_dir.name,
        "engine": engine,
        "legacy_fallback": engine == "qa_player_legacy",
        "legacy_fallback_reason": fallback_reason,
    }


RUNS_DIR = ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)


# --- Resolveur runtime embarque (autonome vs dev) ---
# Trois chemins optionnels dans .env, resolus RELATIVEMENT a ROOT (le repo).
# Si les 3 sont set ET pointent sur des fichiers/dossiers existants,
# /api/replay lance directement `node.exe playwright/cli.js test <spec>` +
# PLAYWRIGHT_BROWSERS_PATH pointe sur les browsers embarques.
# Sinon fallback DEV sur `npx playwright test` global + cache utilisateur.
def _resolve_embedded_runtime() -> Optional[dict]:
    """Retourne {node, cli, browsers, mode="embedded"} si le runtime autonome
    est present et complet, None sinon (le caller fallback sur npx)."""
    node_env = os.getenv("DOMAUTOPSY_NODE_PATH")
    cli_env = os.getenv("DOMAUTOPSY_PLAYWRIGHT_CLI")
    browsers_env = os.getenv("DOMAUTOPSY_BROWSERS_PATH")
    if not (node_env and cli_env and browsers_env):
        return None
    node = (ROOT / node_env).resolve() if not Path(node_env).is_absolute() else Path(node_env)
    cli = (ROOT / cli_env).resolve() if not Path(cli_env).is_absolute() else Path(cli_env)
    browsers = (ROOT / browsers_env).resolve() if not Path(browsers_env).is_absolute() else Path(browsers_env)
    if not (node.exists() and cli.exists() and browsers.exists()):
        # Config presente mais fichiers manquants -> log + fallback dev
        print(f"[server] Runtime embarque configure mais fichiers manquants "
              f"(node={node.exists()}, cli={cli.exists()}, browsers={browsers.exists()}) "
              f"-> fallback npx global")
        return None
    return {"node": str(node), "cli": str(cli), "browsers": str(browsers), "mode": "embedded"}

# --- Config screencast ---
# 1 frame sur N envoyee aux viewers (defaut 2 = ~15fps si CDP delivre 30).
# Plus haut sur ressources contraintes (AKS pod, 10+ viewers concurrents).
SCREENCAST_EVERY_N = int(os.getenv("DOMAUTOPSY_SCREENCAST_EVERY_N", "2"))
SCREENCAST_QUALITY = int(os.getenv("DOMAUTOPSY_SCREENCAST_QUALITY", "60"))
SCREENCAST_MAX_WIDTH = int(os.getenv("DOMAUTOPSY_SCREENCAST_MAX_WIDTH", "1280"))


class ScreencastHub:
    """1 connexion CDP screencast par run -> broadcast a N viewers WebSocket.
    Sans ce hub, chaque viewer ouvrait sa propre session CDP -> N streams
    Chromium = CPU saturation + bandwidth N x. Avec : 1 stream Chromium ->
    fanout en memoire vers les viewers connectes."""
    def __init__(self, run_id: str, cdp_port: int):
        self.run_id = run_id
        self.cdp_port = cdp_port
        self.viewers: set[WebSocket] = set()
        self.last_frame: Optional[dict] = None  # cache pour les nouveaux viewers
        self.task: Optional[asyncio.Task] = None
        self.stopped = False

    async def add_viewer(self, ws: WebSocket):
        self.viewers.add(ws)
        # Envoie immediatement la derniere frame connue au nouveau viewer
        if self.last_frame:
            try:
                await ws.send_json(self.last_frame)
            except Exception:
                pass
        # Demarre la capture CDP au premier viewer
        if self.task is None and not self.stopped:
            self.task = asyncio.create_task(self._capture_loop())

    async def remove_viewer(self, ws: WebSocket):
        self.viewers.discard(ws)

    async def _broadcast(self, frame_msg: dict):
        self.last_frame = frame_msg
        if not self.viewers:
            return
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send_json(frame_msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.viewers.discard(ws)

    async def _capture_loop(self):
        """1 connexion CDP, lit screencastFrame, fan-out vers viewers."""
        page_ws_url = await _wait_for_cdp_page(self.cdp_port, timeout=30)
        if not page_ws_url:
            await self._broadcast({"type": "error", "message": "Chromium CDP indisponible apres 30s"})
            return
        msg_id = 0
        def next_id() -> int:
            nonlocal msg_id
            msg_id += 1
            return msg_id

        timeout = aiohttp.ClientTimeout(total=None)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(page_ws_url, max_msg_size=20 * 1024 * 1024) as cdp_ws:
                    await cdp_ws.send_json({"id": next_id(), "method": "Page.enable"})
                    await cdp_ws.send_json({
                        "id": next_id(),
                        "method": "Page.startScreencast",
                        "params": {
                            "format": "jpeg",
                            "quality": SCREENCAST_QUALITY,
                            "maxWidth": SCREENCAST_MAX_WIDTH,
                            "maxHeight": 720,
                            "everyNthFrame": SCREENCAST_EVERY_N,
                        },
                    })
                    async for ws_msg in cdp_ws:
                        if self.stopped:
                            break
                        if ws_msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(ws_msg.data)
                        if data.get("method") == "Page.screencastFrame":
                            params = data["params"]
                            await self._broadcast({
                                "type": "frame",
                                "data": params["data"],
                                "metadata": params.get("metadata", {}),
                            })
                            await cdp_ws.send_json({
                                "id": next_id(),
                                "method": "Page.screencastFrameAck",
                                "params": {"sessionId": params["sessionId"]},
                            })
        except Exception as e:
            await self._broadcast({"type": "error", "message": f"Capture loop: {e}"})


SCREENCAST_HUBS: dict[str, ScreencastHub] = {}


# Cache des scripts importes : {import_id: {format, original_source, parsed_actions}}
IMPORTS: dict[str, dict] = {}


@app.post("/api/import", dependencies=[Depends(require_token)])
async def import_script(file: UploadFile = File(...)):
    """Recoit un script de test existant (Katalon/PW/Cypress/Selenium),
    le parse, retourne URL detectee + task NL suggeree + format detecte.

    Le source script est conserve en memoire avec un import_id pour le
    futur stage 'diff' (V2.5) ou pour copier le script tel quel si pas
    de drift.
    """
    from script_parser import parse_script, to_nl_task, detect_format

    content = await file.read()
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Fichier non UTF-8")

    if len(source) > 500_000:
        raise HTTPException(400, "Script trop volumineux (max 500KB)")

    fmt = detect_format(file.filename or "")
    if fmt == "unknown":
        raise HTTPException(400, f"Extension non supportee: {file.filename}. Supporte: .groovy .ts .js .py .cy.js .spec.ts")

    parsed = parse_script(file.filename or "uploaded", source, format_hint=fmt)
    if parsed.get("error"):
        raise HTTPException(400, parsed["error"])

    nl_task = to_nl_task(parsed)
    import_id = uuid4().hex[:12]
    IMPORTS[import_id] = {
        "format": parsed["format"],
        "filename": parsed.get("filename", file.filename),
        "url": parsed.get("url"),
        "selectors": parsed.get("selectors", []),
        "actions": parsed.get("actions", []),
        "redacted": parsed.get("redacted", 0),
        "source": source,
    }

    return {
        "import_id": import_id,
        "format": parsed["format"],
        "detected_url": parsed.get("url"),
        "missing_url": parsed.get("url") is None,
        "suggested_task": nl_task,
        "selectors_count": len(parsed.get("selectors", [])),
        "actions_count": len(parsed.get("actions", [])),
        "redacted_count": parsed.get("redacted", 0),
    }


@app.post("/api/run", dependencies=[Depends(require_token)])
async def start_run(req: RunRequest):
    """Lance qa_explorer en sous-process et retourne un run_id"""
    run_id = uuid4().hex[:12]
    cdp_port = find_free_cdp_port()
    # Dossier dedie pour ce run : runs/<timestamp>_<run_id>/
    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / f"{timestamp}_{run_id}"

    args = [
        sys.executable, "-u", str(ROOT / "qa_explorer.py"),
        "--url", req.url,
        "--task", req.task,
        "--output-format", req.output_format,
        "--provider", req.provider,
        "--port", str(cdp_port),
        "--min-wait", str(req.min_wait),
        "--max-wait", str(req.max_wait),
        "--network-idle", str(req.network_idle),
        "--max-steps", str(req.max_steps),
        "--output-dir", str(run_dir),
        "--no-open-report",   # serveur web : on n'ouvre pas le navigateur du serveur
    ]
    if req.model:
        args.extend(["--model", req.model])
    if req.headless:
        args.append("--headless")

    # subprocess.Popen est event-loop-agnostique : marche avec uvicorn --reload sur Windows
    # sans dependance a ProactorEventLoop
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        bufsize=1,           # line-buffered
    )

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    RUNS[run_id] = {
        "proc": proc,
        "cdp_port": cdp_port,
        "log_queue": log_queue,
        "status": "running",
        "report_path": None,
        "url": req.url,
        "task": req.task,
        "output_format": req.output_format,
        "run_dir": str(run_dir),
        "timestamp": timestamp,
    }

    asyncio.create_task(_pump_stdout(run_id, proc, log_queue))
    return {"run_id": run_id, "cdp_port": cdp_port}


async def _pump_stdout(run_id: str, proc: subprocess.Popen, queue: asyncio.Queue):
    """Lit stdout du subprocess ligne a ligne via thread executor (compat any event loop)."""
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    try:
        while True:
            # readline est bloquant -> on le delegue au thread pool de l'event loop
            line_bytes = await loop.run_in_executor(None, proc.stdout.readline)
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            await queue.put(line)
            if "Rapport HTML ->" in line:
                try:
                    path_part = line.split("->", 1)[1].strip()
                    RUNS[run_id]["report_path"] = path_part
                except IndexError:
                    pass
    finally:
        # Attendre que le process termine vraiment (idem, bloquant -> executor)
        await loop.run_in_executor(None, proc.wait)
        RUNS[run_id]["status"] = "exit_" + str(proc.returncode)
        # Post-processing pour les replays : agrege le JSON Playwright dans
        # un rapport HTML self-contained + enrichit le meta.json.
        try:
            r = RUNS.get(run_id) or {}
            if r.get("is_replay") and r.get("run_dir"):
                await loop.run_in_executor(None, _postprocess_replay, run_id, Path(r["run_dir"]))
        except Exception as e:
            await queue.put(f"[server] Post-processing replay echec : {e}")
        await queue.put(None)


def _postprocess_replay(run_id: str, replay_dir: Path) -> None:
    """Genere replay_report.html + enrichit meta.json d'un run replay termine.
    Appele en thread executor pour ne pas bloquer l'event loop.

    Le run source est resolu depuis RUNS[run_id]["source_run_id"] pour que
    le rapport puisse enrichir chaque step_id avec les infos du JSON
    canonique (action, selector, expected/actual, network). Sans ca, on
    n'aurait que les statuts Playwright bruts sans le contexte metier.

    Principe : la generation du rapport est SECONDAIRE. Si elle echoue,
    on log un warning mais on ne transforme jamais un test Playwright
    reussi en echec.
    """
    # Extract subprocess returncode AVANT le try/except pour garantir que
    # update_replay_meta_with_verdict le reçoit meme si generate_replay_report
    # crashe (fix D6 : meta.json ne doit jamais rester "running" bloque).
    r = RUNS.get(run_id) or {}
    proc = r.get("proc")
    returncode = proc.returncode if proc and proc.poll() is not None else None

    try:
        from replay_reporter import generate_replay_report, update_replay_meta_with_verdict
        source_dir = None
        source_run_id = r.get("source_run_id")
        if source_run_id:
            source_dir = _find_run_dir(source_run_id)
        report_path = generate_replay_report(replay_dir, source_run_dir=source_dir)
        if report_path and run_id in RUNS:
            try:
                RUNS[run_id]["report_path"] = str(report_path.relative_to(ROOT))
            except ValueError:
                RUNS[run_id]["report_path"] = str(report_path)
        update_replay_meta_with_verdict(replay_dir, subprocess_returncode=returncode)
    except Exception as e:
        # Warning uniquement - n'affecte pas le verdict du test Playwright.
        # MAIS on doit quand meme mettre a jour meta.json sinon status reste
        # "running" a jamais (bug D6). On retente update seule (sans le report).
        print(f"[server] _postprocess_replay({run_id}) WARNING : {e}")
        try:
            from replay_reporter import update_replay_meta_with_verdict
            update_replay_meta_with_verdict(replay_dir, subprocess_returncode=returncode)
        except Exception as e2:
            print(f"[server] _postprocess_replay({run_id}) meta update ALSO failed : {e2}")


@app.websocket("/ws/logs/{run_id}")
async def ws_logs(ws: WebSocket, run_id: str):
    """Stream stdout subprocess vers le client (1 ligne = 1 message)"""
    await ws.accept()
    if run_id not in RUNS:
        await ws.send_json({"type": "error", "message": f"run_id inconnu: {run_id}"})
        await ws.close()
        return

    queue: asyncio.Queue = RUNS[run_id]["log_queue"]
    try:
        while True:
            line = await queue.get()
            if line is None:
                await ws.send_json({"type": "end", "status": RUNS[run_id]["status"]})
                break
            await ws.send_json({"type": "log", "line": line})
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/screen/{run_id}")
async def ws_screen(ws: WebSocket, run_id: str):
    """Stream les frames Chromium via le ScreencastHub (1 CDP -> N viewers)."""
    await ws.accept()
    if run_id not in RUNS:
        await ws.send_json({"type": "error", "message": f"run_id inconnu: {run_id}"})
        await ws.close()
        return
    cdp_port = RUNS[run_id].get("cdp_port")
    if not cdp_port:
        await ws.send_json({"type": "error", "message": "Ce run n'a pas de CDP (ex: playwright runner sans screencast)"})
        await ws.close()
        return

    # Cree le hub s'il n'existe pas, l'enregistre dans le pool global
    hub = SCREENCAST_HUBS.get(run_id)
    if hub is None:
        hub = ScreencastHub(run_id, cdp_port)
        SCREENCAST_HUBS[run_id] = hub
    await hub.add_viewer(ws)
    try:
        # Garde la connexion ouverte tant que le client est la (pas de message attendu)
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # Ping keepalive
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
            except WebSocketDisconnect:
                break
    except Exception:
        pass
    finally:
        await hub.remove_viewer(ws)
        # Si plus de viewers, on stoppe la capture pour liberer Chromium
        if not hub.viewers:
            hub.stopped = True
            if hub.task:
                hub.task.cancel()
            SCREENCAST_HUBS.pop(run_id, None)


async def _wait_for_cdp_page(cdp_port: int, timeout: float = 30) -> Optional[str]:
    """Poll http://localhost:{cdp_port}/json jusqu'a trouver un target type=page"""
    deadline = asyncio.get_event_loop().time() + timeout
    async with aiohttp.ClientSession() as session:
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with session.get(f"http://localhost:{cdp_port}/json") as resp:
                    targets = await resp.json()
                    for t in targets:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return None


@app.get("/api/report/{run_id}")
async def get_report(run_id: str):
    """Retourne le rapport HTML pour ce run (en cours OU historique).
    Supporte 3 formats de rapport :
      - qa_report_<ts>.html  : runs de capture (qa_explorer)
      - replay_report.html   : runs replay via `npx playwright test` (nouveau)
      - qa_replay_report_<ts>.html : runs replay legacy via qa_player.py"""
    # Cas 1 : run en memoire (in-memory)
    if run_id in RUNS:
        report_path = RUNS[run_id].get("report_path")
        if report_path:
            abs_path = (ROOT / report_path).resolve() if not Path(report_path).is_absolute() else Path(report_path)
            if abs_path.exists():
                return FileResponse(abs_path, media_type="text/html")
    # Cas 2 : run historique (dans runs/ folder)
    candidate = _find_run_dir(run_id)
    if candidate:
        # Ordre de priorite : replay_report (nouveau moteur) -> qa_report
        # (capture) -> qa_replay_report (legacy qa_player). L'ordre couvre
        # tous les runs, indifferemment de leur type.
        replay_direct = candidate / "replay_report.html"
        if replay_direct.exists():
            return FileResponse(replay_direct, media_type="text/html")
        for pattern in ("qa_report_*.html", "qa_replay_report_*.html"):
            reports = list(candidate.glob(pattern))
            if reports:
                return FileResponse(reports[0], media_type="text/html")
    raise HTTPException(404, f"Rapport introuvable pour run_id={run_id}")


def _find_run_dir(run_id: str) -> Optional[Path]:
    """Localise le dossier d'un run dans runs/ par suffixe _<run_id>"""
    if not RUNS_DIR.exists():
        return None
    for d in RUNS_DIR.iterdir():
        if d.is_dir() and d.name.endswith(f"_{run_id}"):
            return d
    return None


@app.get("/api/history")
async def list_history(limit: int = 50):
    """Liste les runs persistes sur disque (lit le meta.json de chaque dossier dans runs/)"""
    if not RUNS_DIR.exists():
        return []
    history = []
    # Tri par date de modif descendant (plus recent d'abord)
    dirs = sorted(
        [d for d in RUNS_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in dirs[:limit]:
        meta_file = d / "meta.json"
        # Extraire le run_id depuis le nom du dossier (format: <timestamp>_<run_id>)
        # Pour les runs CLI, il n'y a pas de run_id, on prend le timestamp comme id
        parts = d.name.split("_", 2)  # YYYYMMDD_HHMMSS_<runid>
        if len(parts) >= 3:
            dir_run_id = parts[2]
        else:
            dir_run_id = d.name   # fallback : nom du dossier complet
        # has_report couvre les 3 formats : qa_report (capture), replay_report
        # (nouveau moteur), qa_replay_report (legacy qa_player). Sans cet
        # elargissement, la sidebar UI ignore les rapports de replay.
        has_qa_report = any(d.glob("qa_report_*.html"))
        has_replay_report = (d / "replay_report.html").exists()
        has_legacy_replay = any(d.glob("qa_replay_report_*.html"))
        entry = {
            "run_id": dir_run_id,
            "dir_name": d.name,
            "has_report": has_qa_report or has_replay_report or has_legacy_replay,
        }
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                entry.update({
                    "timestamp": meta.get("timestamp"),
                    "started_at": meta.get("started_at"),
                    "ended_at": meta.get("ended_at"),
                    "scenario_url": meta.get("scenario_url"),
                    "scenario_name": meta.get("scenario_name"),
                    "task": (meta.get("task") or "")[:120],
                    "output_format": meta.get("output_format"),
                    "provider": meta.get("provider"),
                    "model": meta.get("model"),
                    "agent_result": (meta.get("agent_result") or "")[:200],
                    "deduped_count": meta.get("deduped_count"),
                    "status": meta.get("status"),
                })
            except Exception:
                entry["status"] = "meta_corrupted"
        else:
            entry["status"] = "no_meta"
        history.append(entry)
    return history


@app.get("/api/run/{run_id}/files")
async def list_run_files(run_id: str):
    """Liste les fichiers d'un run (pour l'UI d'historique)"""
    d = None
    if run_id in RUNS and RUNS[run_id].get("run_dir"):
        d = Path(RUNS[run_id]["run_dir"])
    if d is None or not d.exists():
        d = _find_run_dir(run_id)
    if d is None or not d.exists():
        raise HTTPException(404, f"Dossier run introuvable: {run_id}")
    return [
        {"name": f.name, "size": f.stat().st_size}
        for f in sorted(d.iterdir()) if f.is_file()
    ]


@app.get("/api/run/{run_id}/file/{filename}")
async def get_run_file(run_id: str, filename: str):
    """Sert un fichier specifique d'un run (locator_log.json, test_*.groovy, etc.)"""
    # Securite : interdire les path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Nom de fichier invalide")
    d = None
    if run_id in RUNS and RUNS[run_id].get("run_dir"):
        d = Path(RUNS[run_id]["run_dir"])
    if d is None or not d.exists():
        d = _find_run_dir(run_id)
    if d is None or not d.exists():
        raise HTTPException(404, f"Dossier run introuvable: {run_id}")
    target = d / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"Fichier introuvable: {filename}")
    # Type MIME selon l'extension
    media_type = "text/html" if target.suffix == ".html" else (
        "application/json" if target.suffix == ".json" else "text/plain"
    )
    return FileResponse(target, media_type=media_type)


def _run_status_payload(run_id: str) -> dict:
    """Construit le payload de statut JSON pour un run, en cherchant d'abord en
    memoire (run actif ou recemment termine) puis sur disque (meta.json).
    Format pense pour le polling CI (exit_code, verdict, metriques cles)."""
    # Cas 1 : run en memoire
    if run_id in RUNS:
        r = RUNS[run_id]
        proc: Optional[subprocess.Popen] = r.get("proc")
        exit_code = None
        if proc and proc.poll() is not None:
            exit_code = proc.returncode
        status = r["status"]
        is_running = (status == "running") and (proc is None or proc.poll() is None)
        return {
            "run_id": run_id,
            "status": status,
            "is_running": is_running,
            "exit_code": exit_code,
            "verdict": ("success" if exit_code == 0 else ("failure" if exit_code is not None else "running")),
            "cdp_port": r.get("cdp_port"),
            "report_path": r.get("report_path"),
            "url": r.get("url"),
            "task": r.get("task"),
            "output_format": r.get("output_format"),
            "is_replay": r.get("is_replay", False),
            "is_playwright_runner": r.get("is_playwright_runner", False),
            "ci_dashboard_url": f"/ci/{run_id}",
            "log_ws_url": f"/ws/logs/{run_id}",
            "report_url": f"/api/report/{run_id}",
            "source": "memory",
        }
    # Cas 2 : run historique - chercher meta.json sur disque
    d = _find_run_dir(run_id)
    if d is None:
        raise HTTPException(404, f"run_id inconnu: {run_id}")
    meta_file = d / "meta.json"
    if not meta_file.exists():
        # Dossier existe mais pas de meta
        return {
            "run_id": run_id,
            "status": "no_meta",
            "is_running": False,
            "verdict": "unknown",
            "source": "disk",
        }
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"meta.json corrompu : {e}")
    status = meta.get("status") or "unknown"
    # Verdict : si "success" dans meta -> 0, sinon si failure -> 1
    verdict = meta.get("status") if meta.get("status") in ("success", "failure", "killed") else "unknown"
    return {
        "run_id": run_id,
        "status": status,
        "is_running": False,
        "exit_code": 0 if verdict == "success" else (1 if verdict == "failure" else None),
        "verdict": verdict,
        "url": meta.get("scenario_url"),
        "scenario_name": meta.get("scenario_name"),
        "task": meta.get("task"),
        "output_format": meta.get("output_format"),
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "agent_result": meta.get("agent_result"),
        "raw_count": meta.get("raw_count"),
        "deduped_count": meta.get("deduped_count"),
        "js_errors_count": meta.get("js_errors_count"),
        "console_errors_count": meta.get("console_errors_count"),
        "network_count": meta.get("network_count"),
        "network_fail_count": meta.get("network_fail_count"),
        "coverage_pct": meta.get("coverage_pct"),
        "perf_heap_delta_mb": meta.get("perf_heap_delta_mb"),
        "is_replay": meta.get("is_replay", False),
        "is_playwright_runner": meta.get("is_playwright_runner", False),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "ci_dashboard_url": f"/ci/{run_id}",
        "report_url": f"/api/report/{run_id}",
        "source": "disk",
    }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Endpoint REST canonique pour le polling CI.
    Retourne is_running, exit_code, verdict, et toutes les metriques cles.
    Marche pour les runs en cours ET historises (lecture meta.json sur disque).
    """
    return _run_status_payload(run_id)


@app.get("/api/status/{run_id}")
async def run_status(run_id: str):
    """Alias historique de /api/runs/{run_id} (back-compat)"""
    return _run_status_payload(run_id)


@app.get("/ci/{run_id}", response_class=HTMLResponse)
async def ci_dashboard(run_id: str):
    """Page CI publique pour suivre un run en direct depuis l'exterieur.
    Cas d'usage : GitHub Action lance un test via POST /api/playwright/run
    sur un runner dockerise, puis colle l'URL https://host/ci/<run_id> dans
    le job summary pour que tout le monde voie le live log + verdict final.
    """
    html_path = WEB_DIR / "ci.html"
    if not html_path.exists():
        raise HTTPException(404, "ci.html introuvable")
    # On injecte le run_id dans la page (substitution sur le placeholder __RUN_ID__)
    html = html_path.read_text(encoding="utf-8").replace("__RUN_ID__", run_id)
    return HTMLResponse(html)


# Static files (CSS / JS) - tout ce qui est dans web/
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


# ============================================================
# ENTRY POINT : python server.py
# ============================================================
# IMPORTANT Windows : utiliser CE point d'entree (pas `uvicorn server:app`)
# car uvicorn cree son event loop AVANT d'importer server.py, ce qui rend
# le set_event_loop_policy au top du fichier inoperant. Ici, comme on lance
# uvicorn programmatiquement, la policy Proactor est deja en place quand
# uvicorn cree son loop -> create_subprocess_exec marche.
if __name__ == "__main__":
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser(description="DOMAutopsy Web Server")
    parser.add_argument("--host", default="127.0.0.1", help="Adresse d'ecoute (defaut: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port HTTP (defaut: 8000)")
    parser.add_argument("--reload", action="store_true", help="Hot reload pour le dev")
    args = parser.parse_args()
    print(f"[server] Starting on http://{args.host}:{args.port}")
    print(f"[server] Event loop policy: {type(asyncio.get_event_loop_policy()).__name__}")
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        loop="asyncio",  # force le loop standard, pas uvloop (incompatible Windows)
    )
