#!/usr/bin/env python3
"""
DOMAutopsy CLI - Trigger un run a distance + suivi
===================================================
Outil shell pour declencher un run depuis n'importe ou (GitHub Action,
Jenkins, terminal local) et soit attendre le verdict (polling REST),
soit tailer le log en direct (WebSocket).

Le serveur DOMAutopsy doit etre accessible (port 8000 par defaut).
Si DOMAUTOPSY_API_TOKEN est set cote serveur, fournir --token en CLI
ou via env DOMAUTOPSY_API_TOKEN cote client.

Usage :

  # Lance un run browser-use et attend le verdict (CI-friendly)
  python domautopsy_cli.py run \\
      --server https://dom.example.com \\
      --url https://app.example.com/login \\
      --task "Remplis login=demo / pwd=demo, valide" \\
      --format playwright \\
      --token \$DOMAUTOPSY_TOKEN \\
      --wait

  # Lance une suite Playwright native + suivi log live
  python domautopsy_cli.py playwright \\
      --server https://dom.example.com \\
      --project-dir /workspace/my-app/tests \\
      --target login.spec.ts \\
      --args "--workers 1 --grep happy-path" \\
      --token \$DOMAUTOPSY_TOKEN \\
      --tail

  # Rejoue un run historique
  python domautopsy_cli.py replay --server https://dom.example.com \\
      --run-id abc123def456 --wait --token \$DOMAUTOPSY_TOKEN

  # Just polling un run en cours
  python domautopsy_cli.py status --server https://dom.example.com \\
      --run-id abc123def456

Exit codes :
  0 = success (verdict == "success")
  1 = failure (verdict != "success")
  2 = erreur reseau / config
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional


def _http(server: str, path: str, method: str = "GET",
          body: Optional[dict] = None, token: Optional[str] = None,
          timeout: int = 30) -> dict:
    """HTTP helper minimaliste, no-dep (urllib stdlib)."""
    url = server.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[domautopsy-cli] HTTP {e.code} on {method} {url} :\n{err_body}", file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(f"[domautopsy-cli] reseau : {e.reason}", file=sys.stderr)
        sys.exit(2)


def poll_until_done(server: str, run_id: str, token: Optional[str],
                    interval_s: float = 3.0, timeout_s: float = 1800) -> dict:
    """Poll /api/runs/{id} jusqu'a is_running == False, retourne le dernier payload."""
    deadline = time.time() + timeout_s
    last = None
    print(f"[domautopsy-cli] polling /api/runs/{run_id} (every {interval_s}s)...", file=sys.stderr)
    while time.time() < deadline:
        last = _http(server, f"/api/runs/{run_id}", token=token, timeout=10)
        is_running = last.get("is_running", True)
        verdict = last.get("verdict")
        sys.stderr.write(f"  status={last.get('status')} verdict={verdict} {'(in-flight)' if is_running else '(done)'}\n")
        if not is_running:
            return last
        time.sleep(interval_s)
    print(f"[domautopsy-cli] timeout apres {timeout_s}s, dernier payload :", file=sys.stderr)
    print(json.dumps(last, indent=2), file=sys.stderr)
    sys.exit(2)


def tail_log_ws(server: str, run_id: str, token: Optional[str]):
    """Tail le log via WebSocket. Necessite la lib websockets (pip install websockets)."""
    try:
        import asyncio
        import websockets
    except ImportError:
        print("[domautopsy-cli] WARN: lib 'websockets' absente, fallback polling status only.", file=sys.stderr)
        print("                       pip install websockets   pour activer --tail", file=sys.stderr)
        return False
    ws_scheme = "wss" if server.startswith("https") else "ws"
    host = server.replace("https://", "").replace("http://", "").rstrip("/")
    ws_url = f"{ws_scheme}://{host}/ws/logs/{run_id}"
    extra_headers = [("Authorization", f"Bearer {token}")] if token else []

    async def _tail():
        try:
            async with websockets.connect(ws_url, additional_headers=extra_headers) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "log":
                        print(msg["line"])
                    elif msg.get("type") == "end":
                        print(f"[domautopsy-cli] --- end : {msg.get('status')} ---", file=sys.stderr)
                        return msg.get("status") == "exit_0"
                    elif msg.get("type") == "error":
                        print(f"[domautopsy-cli] WS error : {msg.get('message')}", file=sys.stderr)
        except Exception as e:
            print(f"[domautopsy-cli] WS deconnecte : {e}", file=sys.stderr)
            return False
        return None

    return asyncio.run(_tail())


def cmd_run(args):
    payload = {
        "url": args.url,
        "task": args.task,
        "output_format": args.format,
        "provider": args.provider,
        "headless": True,
    }
    if args.model:
        payload["model"] = args.model
    resp = _http(args.server, "/api/run", "POST", payload, args.token)
    run_id = resp["run_id"]
    ci_url = args.server.rstrip("/") + f"/ci/{run_id}"
    print(f"[domautopsy-cli] run_id={run_id}")
    print(f"[domautopsy-cli] live dashboard: {ci_url}")
    if args.tail:
        ok = tail_log_ws(args.server, run_id, args.token)
        if ok is False:
            sys.exit(1)
    if args.wait:
        result = poll_until_done(args.server, run_id, args.token,
                                  interval_s=args.poll_interval, timeout_s=args.poll_timeout)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("verdict") == "success" else 1)


def cmd_playwright(args):
    payload = {
        "project_dir": args.project_dir,
        "target": args.target,
        "args": args.args,
        "headless": True,
    }
    resp = _http(args.server, "/api/playwright/run", "POST", payload, args.token)
    run_id = resp["run_id"]
    ci_url = args.server.rstrip("/") + f"/ci/{run_id}"
    print(f"[domautopsy-cli] run_id={run_id}")
    print(f"[domautopsy-cli] live dashboard: {ci_url}")
    print(f"[domautopsy-cli] cmd: {resp.get('cmd')}")
    if args.tail:
        ok = tail_log_ws(args.server, run_id, args.token)
        if ok is False:
            sys.exit(1)
    if args.wait:
        result = poll_until_done(args.server, run_id, args.token,
                                  interval_s=args.poll_interval, timeout_s=args.poll_timeout)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("verdict") == "success" else 1)


def cmd_replay(args):
    resp = _http(args.server, f"/api/replay/{args.run_id}?headless=true", "POST", None, args.token)
    replay_id = resp["run_id"]
    ci_url = args.server.rstrip("/") + f"/ci/{replay_id}"
    print(f"[domautopsy-cli] replay_run_id={replay_id} (source: {args.run_id})")
    print(f"[domautopsy-cli] live dashboard: {ci_url}")
    if args.tail:
        ok = tail_log_ws(args.server, replay_id, args.token)
        if ok is False:
            sys.exit(1)
    if args.wait:
        result = poll_until_done(args.server, replay_id, args.token,
                                  interval_s=args.poll_interval, timeout_s=args.poll_timeout)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("verdict") == "success" else 1)


def cmd_status(args):
    result = _http(args.server, f"/api/runs/{args.run_id}", token=args.token)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("verdict") == "success" else 1)


def cmd_auth_mint(args):
    """Genere un token fils via le master token. Output : juste le token (ou JSON)."""
    body = {"label": args.label, "ttl_seconds": args.ttl, "scope": args.scope}
    result = _http(args.server, "/api/auth/token", "POST", body, token=args.token)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # Mode pratique pour shell : sort juste le token pour pouvoir faire
        #   export TOKEN=$(python domautopsy_cli.py auth mint --master $MASTER --label foo)
        print(result["token"])


def cmd_auth_list(args):
    """Liste les tokens fils actifs (master token requis)."""
    result = _http(args.server, "/api/auth/tokens", token=args.token)
    print(json.dumps(result, indent=2))


def cmd_auth_revoke(args):
    """Revoque un token fils par son suffix."""
    result = _http(args.server, f"/api/auth/token/{args.suffix}", "DELETE", token=args.token)
    print(json.dumps(result, indent=2))


def cmd_auth_whoami(args):
    """Affiche les infos du token courant."""
    result = _http(args.server, "/api/auth/me", token=args.token)
    print(json.dumps(result, indent=2))


def cmd_auth_genkey(args):
    """Genere un master token aleatoire fort (256 bits d'entropie). Pas de
    connexion serveur, juste un helper local pour remplir .env."""
    import secrets
    nbytes = max(16, args.bytes)
    token = secrets.token_hex(nbytes)
    if args.env:
        print(f"DOMAUTOPSY_API_TOKEN={token}")
    else:
        print(token)


def main():
    parser = argparse.ArgumentParser(
        description="DOMAutopsy CLI : trigger des runs a distance, polling CI ou tail live"
    )
    parser.add_argument("--server", default=os.getenv("DOMAUTOPSY_SERVER", "http://localhost:8000"),
                        help="URL du serveur DOMAutopsy (defaut: $DOMAUTOPSY_SERVER ou localhost:8000)")
    parser.add_argument("--token", default=os.getenv("DOMAUTOPSY_API_TOKEN"),
                        help="Bearer token (defaut: $DOMAUTOPSY_API_TOKEN)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # run
    p_run = sub.add_parser("run", help="Lance un run browser-use")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--format", default="playwright", choices=["katalon", "playwright", "cypress", "selenium"])
    p_run.add_argument("--provider", default="openai", choices=["openai", "groq"])
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--wait", action="store_true", help="Attend la fin du run et exit code = verdict")
    p_run.add_argument("--tail", action="store_true", help="Tail le log via WebSocket en stdout")
    p_run.add_argument("--poll-interval", type=float, default=3.0)
    p_run.add_argument("--poll-timeout", type=float, default=1800)
    p_run.set_defaults(func=cmd_run)

    # playwright
    p_pw = sub.add_parser("playwright", help="Lance 'npx playwright test ...' sur le serveur")
    p_pw.add_argument("--project-dir", required=True, help="Chemin absolu du projet Playwright")
    p_pw.add_argument("--target", default=None, help="Fichier ou dossier (relatif au project-dir)")
    p_pw.add_argument("--args", default=None, help="Args bruts passes a 'npx playwright test'")
    p_pw.add_argument("--wait", action="store_true")
    p_pw.add_argument("--tail", action="store_true")
    p_pw.add_argument("--poll-interval", type=float, default=5.0)
    p_pw.add_argument("--poll-timeout", type=float, default=3600)
    p_pw.set_defaults(func=cmd_playwright)

    # replay
    p_replay = sub.add_parser("replay", help="Rejoue un run historique via Playwright pur")
    p_replay.add_argument("--run-id", required=True)
    p_replay.add_argument("--wait", action="store_true")
    p_replay.add_argument("--tail", action="store_true")
    p_replay.add_argument("--poll-interval", type=float, default=2.0)
    p_replay.add_argument("--poll-timeout", type=float, default=900)
    p_replay.set_defaults(func=cmd_replay)

    # status
    p_status = sub.add_parser("status", help="Affiche le statut JSON d'un run (poll unique)")
    p_status.add_argument("--run-id", required=True)
    p_status.set_defaults(func=cmd_status)

    # auth (groupe de sous-commandes)
    p_auth = sub.add_parser("auth", help="Gestion des tokens (mint, list, revoke, whoami)")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)

    p_mint = auth_sub.add_parser("mint", help="Genere un nouveau token fils (master requis)")
    p_mint.add_argument("--label", required=True, help="Label descriptif (github-run-42, jenkins-nightly, etc.)")
    p_mint.add_argument("--ttl", type=int, default=3600, help="TTL en secondes (defaut: 3600 = 1h)")
    p_mint.add_argument("--scope", default="user", help="Scope (cosmetique: user|readonly)")
    p_mint.add_argument("--json", action="store_true", help="Output JSON complet au lieu du token brut")
    p_mint.set_defaults(func=cmd_auth_mint)

    p_list = auth_sub.add_parser("list", help="Liste les tokens fils actifs (master requis)")
    p_list.set_defaults(func=cmd_auth_list)

    p_revoke = auth_sub.add_parser("revoke", help="Revoque un token fils (master requis)")
    p_revoke.add_argument("--suffix", required=True, help="8 derniers chars du token (donne par list)")
    p_revoke.set_defaults(func=cmd_auth_revoke)

    p_whoami = auth_sub.add_parser("whoami", help="Affiche les infos du token courant")
    p_whoami.set_defaults(func=cmd_auth_whoami)

    p_genkey = auth_sub.add_parser("genkey", help="Genere un master token aleatoire (offline, pas de serveur requis)")
    p_genkey.add_argument("--bytes", type=int, default=32, help="Nombre d'octets (defaut: 32 = 64 chars hex = 256 bits)")
    p_genkey.add_argument("--env", action="store_true", help="Sortie au format 'DOMAUTOPSY_API_TOKEN=...' pour append direct a .env")
    p_genkey.set_defaults(func=cmd_auth_genkey)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
