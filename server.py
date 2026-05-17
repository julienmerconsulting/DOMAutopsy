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


@app.delete("/api/run/{run_id}", dependencies=[Depends(require_token)])
async def kill_run(run_id: str):
    """Tue le subprocess d'un run (cleanup quand l'utilisateur ferme l'onglet)"""
    if run_id not in RUNS:
        raise HTTPException(404, f"run_id inconnu: {run_id}")
    r = RUNS[run_id]
    proc: subprocess.Popen = r["proc"]
    loop = asyncio.get_running_loop()
    if proc.poll() is None:  # encore en vie
        try:
            proc.terminate()
            try:
                # wait via executor + timeout
                await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await loop.run_in_executor(None, proc.wait)
        except ProcessLookupError:
            pass
    r["status"] = "killed"
    return {"run_id": run_id, "status": "killed"}


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
    """Rejoue un run historise via Playwright pur (qa_player.py, pas de LLM).
    Cree un nouveau run dans runs/<ts>_replay_of_<original> et retourne son
    replay_run_id - reutilise les memes WS /ws/logs et /ws/screen que /api/run.
    """
    # Trouver le dossier source
    source_dir = _find_run_dir(run_id)
    if source_dir is None or not source_dir.exists():
        raise HTTPException(404, f"Run source introuvable: {run_id}")
    if not (source_dir / "clean_steps.json").exists():
        raise HTTPException(400, f"clean_steps.json absent dans {source_dir.name}, replay impossible")

    replay_id = uuid4().hex[:12]
    cdp_port = find_free_cdp_port()
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    replay_dir = RUNS_DIR / f"{ts}_replay_{replay_id}"

    args = [
        sys.executable, "-u", str(ROOT / "qa_player.py"),
        "--run-dir", str(source_dir),
        "--output-dir", str(replay_dir),
        "--port", str(cdp_port),
    ]
    if headless:
        args.append("--headless")

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(ROOT), bufsize=1,
    )
    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    RUNS[replay_id] = {
        "proc": proc,
        "cdp_port": cdp_port,
        "log_queue": log_queue,
        "status": "running",
        "report_path": None,
        "url": None,
        "task": f"Replay of {run_id}",
        "output_format": "replay",
        "run_dir": str(replay_dir),
        "timestamp": ts,
        "source_run_id": run_id,
        "is_replay": True,
    }
    asyncio.create_task(_pump_stdout(replay_id, proc, log_queue))
    return {"run_id": replay_id, "cdp_port": cdp_port, "source_run_id": run_id, "replay_dir": replay_dir.name}


RUNS_DIR = ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)

# --- Auth optionnelle via Bearer token ---
# Si DOMAUTOPSY_API_TOKEN est set dans l'env (ou .env), les endpoints qui
# MUTENT l'etat (POST/DELETE) exigent le header 'Authorization: Bearer <token>'.
# Si pas set : no-auth (mode dev/local par defaut).
# GET endpoints + WS + /ci/{id} restent toujours ouverts pour le partage des
# rapports et la lecture publique.
API_TOKEN = os.getenv("DOMAUTOPSY_API_TOKEN", "").strip() or None


async def require_token(authorization: Optional[str] = Header(None)):
    """Dependency qui valide le Bearer token si API_TOKEN est configure."""
    if not API_TOKEN:
        return  # No-auth mode
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token in Authorization header")
    token = authorization.split(None, 1)[1].strip()
    if token != API_TOKEN:
        raise HTTPException(403, "Invalid Bearer token")

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
        await queue.put(None)


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
    """Stream les frames Chromium via CDP screencast vers le client (base64 jpeg)"""
    await ws.accept()
    if run_id not in RUNS:
        await ws.send_json({"type": "error", "message": f"run_id inconnu: {run_id}"})
        await ws.close()
        return

    cdp_port = RUNS[run_id]["cdp_port"]

    # Attendre que Chromium ouvre son endpoint CDP
    page_ws_url = await _wait_for_cdp_page(cdp_port, timeout=30)
    if not page_ws_url:
        await ws.send_json({"type": "error", "message": "Chromium CDP indisponible apres 30s"})
        await ws.close()
        return

    await _stream_screencast(ws, page_ws_url)


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


async def _stream_screencast(client_ws: WebSocket, page_ws_url: str):
    """Connecte au CDP de la page, demarre le screencast, forward chaque frame"""
    msg_id = 0
    def next_id() -> int:
        nonlocal msg_id
        msg_id += 1
        return msg_id

    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.ws_connect(page_ws_url, max_msg_size=20 * 1024 * 1024) as cdp_ws:
                await cdp_ws.send_json({"id": next_id(), "method": "Page.enable"})
                await cdp_ws.send_json({
                    "id": next_id(),
                    "method": "Page.startScreencast",
                    "params": {
                        "format": "jpeg",
                        "quality": 70,
                        "maxWidth": 1280,
                        "maxHeight": 720,
                        "everyNthFrame": 1,
                    },
                })

                async for msg in cdp_ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("method") == "Page.screencastFrame":
                        params = data["params"]
                        # Forward au client
                        try:
                            await client_ws.send_json({
                                "type": "frame",
                                "data": params["data"],
                                "metadata": params.get("metadata", {}),
                            })
                        except (WebSocketDisconnect, RuntimeError):
                            break
                        # Acquitter pour recevoir la frame suivante
                        await cdp_ws.send_json({
                            "id": next_id(),
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params["sessionId"]},
                        })
        except Exception as e:
            try:
                await client_ws.send_json({"type": "error", "message": f"Screencast: {e}"})
            except Exception:
                pass
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass


@app.get("/api/report/{run_id}")
async def get_report(run_id: str):
    """Retourne le rapport HTML genere par qa_explorer pour ce run (en cours OU historique)"""
    # Cas 1 : run en memoire (in-memory)
    if run_id in RUNS:
        report_path = RUNS[run_id].get("report_path")
        if report_path:
            abs_path = (ROOT / report_path).resolve() if not Path(report_path).is_absolute() else Path(report_path)
            if abs_path.exists():
                return FileResponse(abs_path, media_type="text/html")
    # Cas 2 : run historique (dans runs/ folder, on cherche le dossier qui finit par _{run_id})
    candidate = _find_run_dir(run_id)
    if candidate:
        # Trouve le fichier qa_report_*.html dedans
        reports = list(candidate.glob("qa_report_*.html"))
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
        entry = {
            "run_id": dir_run_id,
            "dir_name": d.name,
            "has_report": any(d.glob("qa_report_*.html")),
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
