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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from uuid import uuid4
from typing import Optional
import asyncio
import json
import os
import sys
import socket
import aiohttp
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"

# Plage de ports CDP : 9222 pour le 1er run, 9223 pour le 2eme, etc.
CDP_PORT_BASE = 9222
CDP_PORT_RANGE = 50  # supporte jusqu'a 50 runs en parallele en theorie

app = FastAPI(title="DOMAutopsy Web", version="0.1.0")

# Etat global des runs
RUNS: dict[str, dict] = {}

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


@app.get("/", response_class=HTMLResponse)
async def index():
    """Sert l'UI principale"""
    html_path = WEB_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(404, "index.html introuvable dans web/")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/formats")
async def list_formats():
    """Retourne les formats de sortie supportes (alimente le <select> du frontend)"""
    # Import paresseux pour eviter de charger qa_explorer au boot du serveur
    from qa_explorer import OUTPUT_FORMATS
    return {
        k: {"label": v["label"], "extension": v["extension"]}
        for k, v in OUTPUT_FORMATS.items()
    }


@app.post("/api/run")
async def start_run(req: RunRequest):
    """Lance qa_explorer en sous-process et retourne un run_id"""
    run_id = uuid4().hex[:12]
    cdp_port = find_free_cdp_port()

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
    ]
    if req.model:
        args.extend(["--model", req.model])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(ROOT),
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
    }

    asyncio.create_task(_pump_stdout(run_id, proc, log_queue))
    return {"run_id": run_id, "cdp_port": cdp_port}


async def _pump_stdout(run_id: str, proc: asyncio.subprocess.Process, queue: asyncio.Queue):
    """Lit stdout du subprocess ligne a ligne et push dans la queue. Sentinelle None en fin."""
    assert proc.stdout is not None
    try:
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            await queue.put(line)
            # Detecter la generation du rapport HTML pour le retrouver ensuite
            if "Rapport HTML ->" in line:
                # Extrait le chemin (apres la fleche)
                try:
                    path_part = line.split("->", 1)[1].strip()
                    RUNS[run_id]["report_path"] = path_part
                except IndexError:
                    pass
    finally:
        await proc.wait()
        RUNS[run_id]["status"] = "exit_" + str(proc.returncode)
        await queue.put(None)  # sentinelle


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
    """Retourne le rapport HTML genere par qa_explorer pour ce run"""
    if run_id not in RUNS:
        raise HTTPException(404, f"run_id inconnu: {run_id}")
    report_path = RUNS[run_id].get("report_path")
    if not report_path:
        raise HTTPException(404, "Rapport pas encore genere")
    abs_path = (ROOT / report_path).resolve() if not Path(report_path).is_absolute() else Path(report_path)
    if not abs_path.exists():
        raise HTTPException(404, f"Fichier rapport introuvable: {abs_path}")
    return FileResponse(abs_path, media_type="text/html")


@app.get("/api/status/{run_id}")
async def run_status(run_id: str):
    """Retourne l'etat courant du run"""
    if run_id not in RUNS:
        raise HTTPException(404, f"run_id inconnu: {run_id}")
    r = RUNS[run_id]
    return {
        "run_id": run_id,
        "status": r["status"],
        "cdp_port": r["cdp_port"],
        "report_path": r.get("report_path"),
        "url": r.get("url"),
        "task": r.get("task"),
        "output_format": r.get("output_format"),
    }


# Static files (CSS / JS) - tout ce qui est dans web/
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
